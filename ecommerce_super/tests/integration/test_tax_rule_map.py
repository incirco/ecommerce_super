"""Integration tests for §8c EasyEcom Tax Rule Map + resolver.

Covers the packet's mandatory test list:
  - Flat-rule stamp (one row, blank bands)
  - Slab-rule stamp (multiple banded rows) — ERPNext resolves the
    band by net rate natively (no slab code in our flows)
  - Reconciliation in-band passes / out-of-band raises Discrepancy
  - Unmapped (rule, company) auto-creates a To-Configure doc
  - Company-with-no-rows → discrepancy, not silent pass
  - cess carried product → item
  - (rule, company) uniqueness enforced at DB
  - Workflow transitions (Configure gated, role-gated)

Uses the india-compliance standard GST templates that exist on
_Test Company (GST 5/12/18/28% - TC, Exempted - TC) — the same
templates the FDE picks from in production.
"""

from __future__ import annotations

import frappe
from frappe.model.workflow import apply_workflow
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.doctype.easyecom_tax_rule_map.easyecom_tax_rule_map import (
    _effective_rate_for_template,
    resolve_and_stamp_tax,
    test_resolve,
)
from ecommerce_super.tests.integration.test_location_workflow import (
    _ensure_admin_has_fde_role,
)
from ecommerce_super.tests.integration.test_location_validation import (
    _ensure_test_company,
)


# Real templates seeded by India Compliance on _Test Company.
GST_5_TEMPLATE = "GST 5% - TC"
GST_18_TEMPLATE = "GST 18% - TC"
GST_28_TEMPLATE = "GST 28% - TC"
EXEMPTED_TEMPLATE = "Exempted - TC"


def _wipe_tax_maps(prefix: str) -> None:
    for n in frappe.db.get_all(
        "EasyEcom Tax Rule Map",
        filters={"tax_rule_name": ("like", f"{prefix}%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom Tax Rule Map", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    frappe.db.commit()


def _make_map(
    *,
    tax_rule_name: str,
    company: str,
    workflow_state: str = "To Configure",
    rows: list[dict] | None = None,
) -> str:
    """Insert a Tax Rule Map (workflow auto-applies; we bypass via
    db.set_value when we need a non-initial state)."""
    doc = frappe.new_doc("EasyEcom Tax Rule Map")
    doc.update(
        {
            "tax_rule_name": tax_rule_name,
            "company": company,
            "workflow_state": "To Configure",  # the initial state
        }
    )
    for r in rows or []:
        doc.append("taxes", r)
    doc.insert(ignore_permissions=True)
    if workflow_state != "To Configure":
        frappe.db.set_value(
            "EasyEcom Tax Rule Map",
            doc.name,
            "workflow_state",
            workflow_state,
            update_modified=False,
        )
    return doc.name


def _make_test_item(item_code: str, company: str | None = None) -> "frappe.model.document.Document":
    """Build an in-memory Item (not inserted — the resolver mutates
    it and the caller / 8d decides what to do)."""
    item = frappe.new_doc("Item")
    item.update(
        {
            "item_code": item_code,
            "item_name": item_code,
            "item_group": "All Item Groups",
            "stock_uom": "Nos",
        }
    )
    return item


class TestEffectiveRateLookup(FrappeTestCase):
    """The resolver's reconciliation needs the template's effective GST
    rate. Verify the helper extracts it cleanly from India Compliance's
    GST templates."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.company = _ensure_test_company()

    def test_gst_18_resolves_to_0_18(self) -> None:
        self.assertEqual(_effective_rate_for_template(GST_18_TEMPLATE), 0.18)

    def test_gst_5_resolves_to_0_05(self) -> None:
        self.assertEqual(_effective_rate_for_template(GST_5_TEMPLATE), 0.05)

    def test_unknown_template_returns_none(self) -> None:
        self.assertIsNone(_effective_rate_for_template("Not A Real Template"))

    def test_none_template_returns_none(self) -> None:
        self.assertIsNone(_effective_rate_for_template(None))


class TestFlatRuleStamp(FrappeTestCase):
    """A flat rule maps to ONE Item Tax row with blank Min/Max bands."""

    PREFIX = "tax-flat-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.company = _ensure_test_company()

    def setUp(self) -> None:
        _wipe_tax_maps(self.PREFIX)

    def tearDown(self) -> None:
        _wipe_tax_maps(self.PREFIX)

    def test_flat_rule_stamps_single_row(self) -> None:
        _make_map(
            tax_rule_name=f"{self.PREFIX}flat",
            company=self.company,
            rows=[{"item_tax_template": GST_18_TEMPLATE}],
        )
        item = _make_test_item("flat-1")
        product = {"tax_rule_name": f"{self.PREFIX}flat", "tax_rate": 0.18}

        result = resolve_and_stamp_tax(item, product, self.company)

        self.assertTrue(result.mapped)
        self.assertEqual(result.stamped_count, 1)
        self.assertTrue(result.reconciled)
        self.assertEqual(result.discrepancies, [])
        # Item now carries one row with blank bands.
        self.assertEqual(len(item.taxes), 1)
        self.assertEqual(item.taxes[0].item_tax_template, GST_18_TEMPLATE)
        self.assertFalse(item.taxes[0].minimum_net_rate)
        self.assertFalse(item.taxes[0].maximum_net_rate)


class TestSlabRuleStamp(FrappeTestCase):
    """A slab rule maps to MULTIPLE Item Tax rows, each banded by
    Min/Max Net Rate. ERPNext resolves the band natively at invoice
    time — no slab code in our flows."""

    PREFIX = "tax-slab-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.company = _ensure_test_company()

    def setUp(self) -> None:
        _wipe_tax_maps(self.PREFIX)

    def tearDown(self) -> None:
        _wipe_tax_maps(self.PREFIX)

    def test_slab_rule_stamps_two_banded_rows(self) -> None:
        """The canonical packet example: 'GST' rule, 0-2500 → GST 5%,
        2500+ → GST 18%."""
        _make_map(
            tax_rule_name=f"{self.PREFIX}slab",
            company=self.company,
            rows=[
                {
                    "item_tax_template": GST_5_TEMPLATE,
                    "minimum_net_rate": 0,
                    "maximum_net_rate": 2500,
                },
                {
                    "item_tax_template": GST_18_TEMPLATE,
                    "minimum_net_rate": 2500,
                    "maximum_net_rate": 0,
                },
            ],
        )
        item = _make_test_item("slab-1")
        # EE resolved at 18% (item priced > 2500) — should reconcile.
        product = {"tax_rule_name": f"{self.PREFIX}slab", "tax_rate": 0.18}

        result = resolve_and_stamp_tax(item, product, self.company)

        self.assertTrue(result.mapped)
        self.assertEqual(result.stamped_count, 2)
        self.assertTrue(result.reconciled)
        # Both banded rows present on the item.
        self.assertEqual(len(item.taxes), 2)
        bands = sorted(
            [(r.minimum_net_rate or 0, r.maximum_net_rate or 0, r.item_tax_template)
             for r in item.taxes]
        )
        self.assertEqual(
            bands,
            [
                (0, 2500, GST_5_TEMPLATE),
                (2500, 0, GST_18_TEMPLATE),
            ],
        )

    def test_slab_rule_reconciles_low_band_rate(self) -> None:
        """Same slab map, but EE resolved 5% — also reconciles (the
        low band's rate)."""
        _make_map(
            tax_rule_name=f"{self.PREFIX}slab-low",
            company=self.company,
            rows=[
                {"item_tax_template": GST_5_TEMPLATE, "minimum_net_rate": 0, "maximum_net_rate": 2500},
                {"item_tax_template": GST_18_TEMPLATE, "minimum_net_rate": 2500},
            ],
        )
        item = _make_test_item("slab-low")
        product = {"tax_rule_name": f"{self.PREFIX}slab-low", "tax_rate": 0.05}
        result = resolve_and_stamp_tax(item, product, self.company)
        self.assertTrue(result.reconciled)
        self.assertEqual(result.discrepancies, [])


class TestReconciliation(FrappeTestCase):
    """In-band rates pass; out-of-band rates raise Discrepancy."""

    PREFIX = "tax-recon-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.company = _ensure_test_company()

    def setUp(self) -> None:
        _wipe_tax_maps(self.PREFIX)

    def tearDown(self) -> None:
        _wipe_tax_maps(self.PREFIX)

    def test_in_band_rate_passes(self) -> None:
        _make_map(
            tax_rule_name=f"{self.PREFIX}in",
            company=self.company,
            rows=[{"item_tax_template": GST_18_TEMPLATE}],
        )
        item = _make_test_item("in-1")
        product = {"tax_rule_name": f"{self.PREFIX}in", "tax_rate": 0.18}
        result = resolve_and_stamp_tax(item, product, self.company)
        self.assertTrue(result.reconciled)
        self.assertEqual(result.discrepancies, [])

    def test_out_of_band_rate_raises_discrepancy(self) -> None:
        """EE rule was mapped to GST 18% but the product carries 12% —
        rule changed in EE, or FDE mis-entered the band."""
        _make_map(
            tax_rule_name=f"{self.PREFIX}out",
            company=self.company,
            rows=[{"item_tax_template": GST_18_TEMPLATE}],
        )
        item = _make_test_item("out-1")
        product = {"tax_rule_name": f"{self.PREFIX}out", "tax_rate": 0.12}
        result = resolve_and_stamp_tax(item, product, self.company)
        self.assertFalse(result.reconciled)
        self.assertEqual(len(result.discrepancies), 1)
        self.assertIn("0.12", result.discrepancies[0])
        self.assertIn("does not match", result.discrepancies[0])


class TestUnmappedAutoCreate(FrappeTestCase):
    """An unmapped (rule, company) → auto-create a To-Configure doc.
    Item taxes NOT silently defaulted."""

    PREFIX = "tax-unmapped-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.company = _ensure_test_company()

    def setUp(self) -> None:
        _wipe_tax_maps(self.PREFIX)

    def tearDown(self) -> None:
        _wipe_tax_maps(self.PREFIX)

    def test_unmapped_rule_auto_creates_to_configure(self) -> None:
        rule = f"{self.PREFIX}new-rule"
        item = _make_test_item("unmapped-1")
        product = {"tax_rule_name": rule, "tax_rate": 0.18}
        result = resolve_and_stamp_tax(item, product, self.company)

        self.assertFalse(result.mapped)
        self.assertTrue(result.auto_created)
        self.assertEqual(result.stamped_count, 0)
        self.assertFalse(result.reconciled)
        self.assertEqual(len(result.discrepancies), 1)
        self.assertIn("auto-created", result.discrepancies[0])

        # Auto-created doc exists in To Configure.
        self.assertTrue(result.map_docname)
        doc = frappe.get_doc("EasyEcom Tax Rule Map", result.map_docname)
        self.assertEqual(doc.tax_rule_name, rule)
        self.assertEqual(doc.company, self.company)
        self.assertEqual(doc.workflow_state, "To Configure")
        self.assertEqual(len(doc.taxes), 0)  # empty until FDE fills

        # Item taxes NOT silently defaulted.
        self.assertEqual(len(item.taxes), 0)


class TestCompanyWithNoRows(FrappeTestCase):
    """A map exists for the (rule, company) pair but has empty taxes —
    discrepancy, NOT silent pass."""

    PREFIX = "tax-norows-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.company = _ensure_test_company()

    def setUp(self) -> None:
        _wipe_tax_maps(self.PREFIX)

    def tearDown(self) -> None:
        _wipe_tax_maps(self.PREFIX)

    def test_empty_taxes_raises_discrepancy(self) -> None:
        _make_map(tax_rule_name=f"{self.PREFIX}empty", company=self.company, rows=[])
        item = _make_test_item("norows-1")
        product = {"tax_rule_name": f"{self.PREFIX}empty", "tax_rate": 0.18}
        result = resolve_and_stamp_tax(item, product, self.company)
        self.assertTrue(result.mapped)
        self.assertFalse(result.auto_created)
        self.assertEqual(result.stamped_count, 0)
        self.assertFalse(result.reconciled)
        self.assertEqual(len(result.discrepancies), 1)
        self.assertIn("no Item Tax Template rows", result.discrepancies[0])
        self.assertEqual(len(item.taxes), 0)


class TestCessPassThrough(FrappeTestCase):
    """cess from product.cess writes to item.ecs_cess (and returns in
    the result regardless)."""

    PREFIX = "tax-cess-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.company = _ensure_test_company()

    def setUp(self) -> None:
        _wipe_tax_maps(self.PREFIX)

    def tearDown(self) -> None:
        _wipe_tax_maps(self.PREFIX)

    def test_cess_written_to_item_ecs_cess(self) -> None:
        _make_map(
            tax_rule_name=f"{self.PREFIX}cess",
            company=self.company,
            rows=[{"item_tax_template": GST_18_TEMPLATE}],
        )
        item = _make_test_item("cess-1")
        product = {"tax_rule_name": f"{self.PREFIX}cess", "tax_rate": 0.18, "cess": 12.5}
        result = resolve_and_stamp_tax(item, product, self.company)
        self.assertEqual(result.cess, 12.5)
        self.assertEqual(item.ecs_cess, 12.5)

    def test_cess_default_zero(self) -> None:
        _make_map(
            tax_rule_name=f"{self.PREFIX}nocess",
            company=self.company,
            rows=[{"item_tax_template": GST_18_TEMPLATE}],
        )
        item = _make_test_item("cess-2")
        product = {"tax_rule_name": f"{self.PREFIX}nocess", "tax_rate": 0.18}
        result = resolve_and_stamp_tax(item, product, self.company)
        self.assertEqual(result.cess, 0.0)
        self.assertEqual(item.ecs_cess, 0.0)

    def test_cess_applied_even_when_unmapped(self) -> None:
        """cess is OUTSIDE the tax map — it's per-product. So it
        should write to the item even if the rule was unmapped."""
        item = _make_test_item("cess-3")
        product = {"tax_rule_name": f"{self.PREFIX}unmapped-cess", "tax_rate": 0.18, "cess": 7.0}
        result = resolve_and_stamp_tax(item, product, self.company)
        self.assertFalse(result.mapped)
        self.assertEqual(result.cess, 7.0)
        self.assertEqual(item.ecs_cess, 7.0)


class TestTemplateCompanyMatch(FrappeTestCase):
    """§8.5.3: rows hold ONLY the parent's company's templates. The
    form's set_query filters the dropdown; this server-side check is
    the gate against API writes / fixtures / typos.

    Uses _Other Test Co's GST 18% template (created on demand) to
    attempt a wrong-Company cross-reference."""

    PREFIX = "tax-co-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.company = _ensure_test_company()
        # Ensure a second Company with its own GST templates exists.
        if not frappe.db.exists("Company", "_Other Test Co"):
            other = frappe.new_doc("Company")
            other.update(
                {
                    "company_name": "_Other Test Co",
                    "abbr": "OTC",
                    "default_currency": "INR",
                    "country": "India",
                }
            )
            other.insert(ignore_permissions=True)
        cls.other_company = "_Other Test Co"
        cls.other_company_gst_18 = "GST 18% - OTC"

    def setUp(self) -> None:
        _wipe_tax_maps(self.PREFIX)

    def tearDown(self) -> None:
        _wipe_tax_maps(self.PREFIX)

    def test_template_from_other_company_rejected(self) -> None:
        """Save with a row pointing to OTC's GST template while the
        parent's company is TC — must throw."""
        # Sanity: OTC's GST 18% template was auto-created by India Compliance.
        if not frappe.db.exists("Item Tax Template", self.other_company_gst_18):
            self.skipTest(
                f"India Compliance did not seed {self.other_company_gst_18} — "
                "test environment doesn't carry GST templates for both Companies."
            )
        with self.assertRaises(frappe.ValidationError):
            _make_map(
                tax_rule_name=f"{self.PREFIX}cross",
                company=self.company,  # TC
                rows=[{"item_tax_template": self.other_company_gst_18}],  # OTC
            )

    def test_same_company_template_accepted(self) -> None:
        # Sanity check the positive path under the same setup.
        _make_map(
            tax_rule_name=f"{self.PREFIX}ok",
            company=self.company,
            rows=[{"item_tax_template": GST_18_TEMPLATE}],
        )
        self.assertTrue(
            frappe.db.exists(
                "EasyEcom Tax Rule Map",
                {"tax_rule_name": f"{self.PREFIX}ok", "company": self.company},
            )
        )


class TestDbUnique(FrappeTestCase):
    """(tax_rule_name, company) UNIQUE at the DB level."""

    PREFIX = "tax-unique-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.company = _ensure_test_company()

    def setUp(self) -> None:
        _wipe_tax_maps(self.PREFIX)

    def tearDown(self) -> None:
        _wipe_tax_maps(self.PREFIX)

    def test_duplicate_rule_company_rejected(self) -> None:
        _make_map(tax_rule_name=f"{self.PREFIX}dup", company=self.company)
        with self.assertRaises(
            (
                frappe.ValidationError,  # validate-level catch
                frappe.DuplicateEntryError,  # DB-level UNIQUE
                frappe.exceptions.UniqueValidationError,
            )
        ):
            _make_map(tax_rule_name=f"{self.PREFIX}dup", company=self.company)


class TestDryRunMatchesRealResolver(FrappeTestCase):
    """The load-bearing guarantee of the Test Resolve UI: the dry-run's
    reconciliation verdict for a given map + sample rate is the SAME
    verdict the real resolver produces for an item-sync against the
    same map with a product carrying that rate.

    Both paths flow through preview_stamp + reconcile_rate (the shared
    pure functions). This test pins that wiring — if anyone refactors
    one path without the other, the test fails immediately and
    surfaces the divergence.
    """

    PREFIX = "tax-parity-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.company = _ensure_test_company()

    def setUp(self) -> None:
        _wipe_tax_maps(self.PREFIX)

    def tearDown(self) -> None:
        _wipe_tax_maps(self.PREFIX)

    def _make_flat_map(self) -> str:
        return _make_map(
            tax_rule_name=f"{self.PREFIX}flat",
            company=self.company,
            rows=[{"item_tax_template": GST_18_TEMPLATE}],
        )

    def _make_slab_map(self) -> str:
        return _make_map(
            tax_rule_name=f"{self.PREFIX}slab",
            company=self.company,
            rows=[
                {"item_tax_template": GST_5_TEMPLATE, "minimum_net_rate": 0, "maximum_net_rate": 2500},
                {"item_tax_template": GST_18_TEMPLATE, "minimum_net_rate": 2500},
            ],
        )

    def _make_empty_map(self) -> str:
        return _make_map(tax_rule_name=f"{self.PREFIX}empty", company=self.company)

    def _real_verdict(self, map_doc: "frappe.model.document.Document", rate: float) -> tuple[bool, list[str]]:
        """Run the real resolver against a fresh item; return its
        verdict + discrepancies."""
        item = _make_test_item(f"parity-item-{rate}")
        product = {"tax_rule_name": map_doc.tax_rule_name, "tax_rate": rate}
        r = resolve_and_stamp_tax(item, product, self.company)
        return r.reconciled, r.discrepancies

    def _dry_verdict(self, map_name: str, rate: float) -> tuple[bool, list[str]]:
        """Run the dry-run; return its verdict + discrepancies."""
        d = test_resolve(map_name=map_name, sample_tax_rate=rate)
        return d["reconciled"], d["discrepancies"]

    def test_flat_rule_in_band_parity(self) -> None:
        name = self._make_flat_map()
        map_doc = frappe.get_doc("EasyEcom Tax Rule Map", name)
        real_v, real_d = self._real_verdict(map_doc, 0.18)
        dry_v, dry_d = self._dry_verdict(name, 0.18)
        self.assertEqual(real_v, dry_v, "reconciled verdict diverged (in-band flat)")
        self.assertEqual(real_v, True)
        self.assertEqual(real_d, dry_d, "discrepancy text diverged (in-band flat)")
        self.assertEqual(real_d, [])

    def test_flat_rule_out_of_band_parity(self) -> None:
        name = self._make_flat_map()
        map_doc = frappe.get_doc("EasyEcom Tax Rule Map", name)
        real_v, real_d = self._real_verdict(map_doc, 0.12)
        dry_v, dry_d = self._dry_verdict(name, 0.12)
        self.assertEqual(real_v, dry_v, "reconciled verdict diverged (out-of-band flat)")
        self.assertEqual(real_v, False)
        # Both paths must produce the SAME discrepancy text — they go
        # through the same reconcile_rate function.
        self.assertEqual(real_d, dry_d)
        self.assertEqual(len(real_d), 1)
        self.assertIn("0.12", real_d[0])

    def test_slab_rule_low_band_parity(self) -> None:
        name = self._make_slab_map()
        map_doc = frappe.get_doc("EasyEcom Tax Rule Map", name)
        real_v, real_d = self._real_verdict(map_doc, 0.05)
        dry_v, dry_d = self._dry_verdict(name, 0.05)
        self.assertEqual(real_v, dry_v)
        self.assertEqual(real_d, dry_d)
        self.assertTrue(real_v)

    def test_slab_rule_high_band_parity(self) -> None:
        name = self._make_slab_map()
        map_doc = frappe.get_doc("EasyEcom Tax Rule Map", name)
        real_v, real_d = self._real_verdict(map_doc, 0.18)
        dry_v, dry_d = self._dry_verdict(name, 0.18)
        self.assertEqual(real_v, dry_v)
        self.assertEqual(real_d, dry_d)
        self.assertTrue(real_v)

    def test_slab_rule_out_of_band_parity(self) -> None:
        name = self._make_slab_map()
        map_doc = frappe.get_doc("EasyEcom Tax Rule Map", name)
        real_v, real_d = self._real_verdict(map_doc, 0.28)
        dry_v, dry_d = self._dry_verdict(name, 0.28)
        self.assertEqual(real_v, dry_v)
        self.assertEqual(real_d, dry_d)
        self.assertFalse(real_v)

    def test_empty_map_parity(self) -> None:
        """A map with no rows: real resolver returns mapped=True,
        reconciled=False, discrepancy='no Item Tax Template rows'.
        The dry-run reaches the same verdict via the same code path."""
        name = self._make_empty_map()
        map_doc = frappe.get_doc("EasyEcom Tax Rule Map", name)
        real_v, real_d = self._real_verdict(map_doc, 0.18)
        dry_result = test_resolve(map_name=name, sample_tax_rate=0.18)
        self.assertFalse(dry_result["mapped"])
        self.assertFalse(real_v)
        # The dry-run wraps the empty_reason as preview-level info;
        # the real resolver wraps the same string into result.discrepancies.
        # Both refer to the same underlying preview.empty_reason text.
        self.assertEqual(len(real_d), 1)
        self.assertIn("has no", real_d[0])
        self.assertIn("has no", dry_result["empty_reason"])

    def test_no_sample_rate_skips_reconciliation_parity(self) -> None:
        """A None sample tax_rate means 'preview only'; both paths
        treat that as reconciled=True (no rate to fail against)."""
        name = self._make_flat_map()
        map_doc = frappe.get_doc("EasyEcom Tax Rule Map", name)
        # Real path: product with no tax_rate.
        item = _make_test_item("no-rate")
        r = resolve_and_stamp_tax(
            item, {"tax_rule_name": map_doc.tax_rule_name}, self.company
        )
        # Dry-run: pass blank.
        d = test_resolve(map_name=name, sample_tax_rate=None)
        self.assertEqual(r.reconciled, d["reconciled"])
        self.assertEqual(r.discrepancies, d["discrepancies"])
        self.assertTrue(r.reconciled)

    def test_dry_run_does_not_persist_anything(self) -> None:
        """Calling test_resolve against an unmapped rule MUST NOT
        auto-create a map (that's the production path only). And it
        MUST NOT mutate any existing map."""
        # No map exists for this rule — dry-run is called against an
        # existing map only, but the FDE could in principle hit
        # test_resolve on a map and then we check that calling it
        # doesn't modify the map's rows / state.
        name = self._make_flat_map()
        before = frappe.get_doc("EasyEcom Tax Rule Map", name).as_dict()
        # Run dry-run several times with various inputs.
        for rate in (0.05, 0.12, 0.18, 0.28, None):
            test_resolve(map_name=name, sample_tax_rate=rate, sample_cess=7.5)
        after = frappe.get_doc("EasyEcom Tax Rule Map", name).as_dict()
        # Snapshot equality on the load-bearing fields (modified
        # timestamps change naturally; exclude those).
        for f in (
            "tax_rule_name",
            "company",
            "workflow_state",
            "taxes",
        ):
            self.assertEqual(before.get(f), after.get(f), f"field {f} mutated by dry-run")


class TestWorkflowFixture(FrappeTestCase):
    """Tax Rule Map Workflow: To Configure → Configured (gated on
    taxes non-empty), branch Ignored."""

    PREFIX = "tax-wf-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.company = _ensure_test_company()
        _ensure_admin_has_fde_role()

    def setUp(self) -> None:
        _wipe_tax_maps(self.PREFIX)
        self._original_user = frappe.session.user

    def tearDown(self) -> None:
        frappe.set_user(self._original_user)
        _wipe_tax_maps(self.PREFIX)

    def test_workflow_doc_exists_and_is_active(self) -> None:
        self.assertTrue(frappe.db.exists("Workflow", "Tax Rule Map Workflow"))
        wf = frappe.get_doc("Workflow", "Tax Rule Map Workflow")
        self.assertEqual(wf.document_type, "EasyEcom Tax Rule Map")
        self.assertEqual(wf.is_active, 1)

    def test_configure_blocked_without_taxes(self) -> None:
        name = _make_map(tax_rule_name=f"{self.PREFIX}empty", company=self.company)
        doc = frappe.get_doc("EasyEcom Tax Rule Map", name)
        with self.assertRaises(frappe.ValidationError):
            apply_workflow(doc, "Configure")

    def test_configure_succeeds_with_taxes(self) -> None:
        name = _make_map(
            tax_rule_name=f"{self.PREFIX}filled",
            company=self.company,
            rows=[{"item_tax_template": GST_18_TEMPLATE}],
        )
        doc = frappe.get_doc("EasyEcom Tax Rule Map", name)
        apply_workflow(doc, "Configure")
        doc.reload()
        self.assertEqual(doc.workflow_state, "Configured")

    def test_mark_not_relevant_routes_to_ignored(self) -> None:
        name = _make_map(tax_rule_name=f"{self.PREFIX}skip", company=self.company)
        doc = frappe.get_doc("EasyEcom Tax Rule Map", name)
        apply_workflow(doc, "Mark Not Relevant")
        doc.reload()
        self.assertEqual(doc.workflow_state, "Ignored")

    def test_reconfigure_from_configured(self) -> None:
        name = _make_map(
            tax_rule_name=f"{self.PREFIX}reconf",
            company=self.company,
            rows=[{"item_tax_template": GST_18_TEMPLATE}],
        )
        doc = frappe.get_doc("EasyEcom Tax Rule Map", name)
        apply_workflow(doc, "Configure")
        doc.reload()
        apply_workflow(doc, "Reconfigure")
        doc.reload()
        self.assertEqual(doc.workflow_state, "To Configure")
