# Playbook: Methodology Migration

**When to use this:** A methodology rule has changed from one Default to a new Default. Existing clients on the old rule need to be migrated to the new rule. Examples: a tolerance threshold tightened from 0.5% to 0.2%, an Account Role Map mapping was corrected, a new GST disposition rule supersedes an older one.

**Audience:** This is FDE-led work; methodology team owns the migration playbook content; Claude Code may be asked to help script the migration logic or scaffold the per-client config changes.

**Pre-flight check:** Read `BRD.md` Section 10 (The Methodology Lifecycle). Methodology migrations must follow the lifecycle. Not every rule change is a "migration" — only changes from Default to a new Default trigger this playbook.

---

## What "methodology migration" actually involves

Three concurrent flows, each with different cadence:

1. **Code/fixture flow** — the new Default ships in the parent app
2. **Per-client transition flow** — each existing client moves from the old Default to the new Default
3. **Documentation flow** — the change is recorded in `CHANGELOG.md`, the methodology team's review minutes, and (if behaviorally different) in any client `methodology_addendum.md`

The three flows happen on different timescales. The code change ships in one release. The per-client transition takes 2-3 months across the FDE fleet. The documentation flow runs continuously.

---

## The 8-step workflow

### 1. Confirm methodology team approval

Verify the rule is genuinely Default-to-Default per `BRD.md` Section 10.2:

- It has been validated against ≥3 FDE-fleet clients for ≥2 months as Pilot
- The validation report shows: rule produced expected behavior, no false positives, recovered/avoided value > implementation cost
- Two methodology team members have reviewed and signed off

If the rule hasn't met this bar, **stop**. Migrating clients to a Pilot-state rule is bad methodology — the rule may be retracted before the migration completes.

### 2. Author the migration playbook

Per `BRD.md` Section 10.3: every Default-to-Default deprecation requires a migration playbook in `docs/playbooks/methodology_migration_<rule_name>.md` (sub-playbook of this one).

The sub-playbook captures, for the specific rule:

- **What changed**: prose description of the old rule vs new rule
- **Why it changed**: methodology team's reasoning (with link to review minutes)
- **Effective date for new clients**: when the new Default takes effect for new deployments
- **Migration window**: the deadline by which existing clients should migrate (typically 2-3 months)
- **Per-client migration steps**: what an FDE does to migrate one client
- **Behavioral diff**: what the client sees differently after migration (in numbers — e.g., "client's monthly Discrepancy count expected to increase 15% due to tightened tolerance, but recoverable value also expected to increase")
- **Rollback procedure**: how to reverse the migration if it goes wrong on a specific client

### 3. Update parent-app fixtures

Add or update the relevant fixture(s) for the new Default:

- For an Account Role Map change: edit `account_role_map.json` with the corrected entry
- For a tolerance change: edit the relevant SLA Budget or recon configuration fixture
- For a GST disposition change: edit `easyecom_field_mapping.json` (if affecting payload translation) or the methodology Python module (if affecting downstream computation)

The fixture change must be backwards-compatible during the migration window. **The new Default ships in code, but each client's existing rule must continue to work until the FDE migrates them.**

This is achieved with a per-client override mechanism. The flow:
- Parent app fixture has the new Default
- Until a client is migrated, their `client_overrides.json` carries an explicit "use the old value" override
- During migration, the FDE removes the override

### 4. Stamp the deprecation

Update the methodology lifecycle metadata. In the methodology Python module's defaults file (e.g., `methodology/disposition_rules.py`):

```python
# Methodology v0.2 — supersedes v0.1's HSN tolerance
TOLERANCE_HSN_TAX_VARIANCE_PCT = 0.2
TOLERANCE_HSN_TAX_VARIANCE_PCT_DEPRECATED_v0_1 = 1.0  # retained for reference

# Each rule that's deprecated has an entry in METHODOLOGY_DEPRECATIONS:
METHODOLOGY_DEPRECATIONS = {
    "tolerance_hsn_tax_variance": {
        "old_value": 1.0,
        "new_value": 0.2,
        "deprecated_at": "2026-05-15",
        "removal_at": "2026-08-15",
        "migration_playbook": "docs/playbooks/methodology_migration_hsn_tolerance.md",
    },
    ...
}
```

The Morning Brief reads `METHODOLOGY_DEPRECATIONS` to surface deprecation warnings to FDEs of clients still on the old rule. Per `BRD.md` Section 10.3.

### 5. Test the migration logic

In `apps/ecommerce_super/ecommerce_super/tests/methodology/test_<rule_name>_migration.py`:

```python
class TestHsnToleranceMigration(FrappeTestCase):
    def test_old_default_still_works_with_override(self):
        # Client with explicit override of the old value
        # Behavior should match v0.1
        ...

    def test_new_default_applies_when_no_override(self):
        # Client with no override
        # Behavior should match v0.2
        ...

    def test_morning_brief_warns_clients_on_old_default(self):
        # If a client's effective rule still matches the old Default,
        # the Morning Brief should include a deprecation warning
        ...
```

### 6. FDE-led per-client migration

For each client on the old rule, the FDE:

a. Opens the client's deployment, reviews their current configuration
b. Opens the methodology migration sub-playbook
c. Reviews the behavioral diff with the client (especially if Discrepancy patterns will change)
d. Runs a dry-run analysis: applies the new rule against the client's last 3 months of data, shows what would have been different
e. Schedules a cutover date with the client (typically a Monday morning)
f. On cutover: removes the override from the client's `client_overrides.json`, runs `bench migrate`, verifies behavior on a single test event
g. Monitors the client's first week of activity post-cutover; reports any unexpected outcomes to methodology team

Some migrations require Configuration Audit entries explaining the cutover (per `SPEC.md` Section 28). These are auto-generated when the override is removed via the desk; if removed via fixture edit, the FDE must manually create a Configuration Audit row.

### 7. Update the per-client methodology addendum

If the migration introduces a per-client deviation from the new Default (rare but possible — e.g., the client negotiated to keep the old tolerance for another quarter), update `apps/ecommerce_super_<client>/methodology_addendum.md`:

```markdown
## 2026-05-15 — HSN tax variance tolerance

The standard methodology v0.2 sets HSN tax variance tolerance to 0.2%. This client has negotiated to retain v0.1's 1.0% tolerance until 2026-08-31 due to ongoing supplier-side tax data quality issues. After 2026-08-31, this client migrates to the standard 0.2%.

Approved by: <methodology lead name>, <date>
Override location: client_overrides.json:tolerance_hsn_tax_variance
```

The addendum is the per-client paper trail. Methodology audits read this file; engineering reviews of client apps reference it.

### 8. Track migration completion

The methodology team tracks per-FDE-fleet migration progress in a tracking table (typically a Google Sheet or Notion database; not a Frappe DocType because it's cross-tenant):

| Client | Old rule | New rule | Migration date scheduled | Migrated | Notes |
| --- | --- | --- | --- | --- | --- |
| Acme Corp | v0.1 | v0.2 | 2026-06-15 | Yes (2026-06-15) | Clean migration, no anomalies |
| Bravo Ltd | v0.1 | v0.2 | 2026-06-22 | No | FDE on leave; rescheduled to 2026-07-01 |

When all FDE-fleet clients have migrated (or formally opted to retain the old rule via approved addendum), the rule's `removal_at` date is honored. After that date:

- The DEPRECATED entry is removed from `METHODOLOGY_DEPRECATIONS`
- The compatibility code paths are removed
- Any client still on the old rule (without an approved addendum) becomes out of compliance — engineering escalates

---

## Common patterns of methodology change

### Tolerance tightening

Old: 1% tolerance on Order-to-Settlement amount variance.
New: 0.5% tolerance.

Effect: more Discrepancies will be raised. Clients see more alerts. Migration playbook explains: "you'll see ~30% more Amount Mismatch Discrepancies after migration. The recoverable value historically associated with these is ₹X per ₹1L of GMV."

The FDE's job during migration: reassure the client that more Discrepancies isn't worse — it's better detection of leakage that was previously absorbed silently.

### Account Role Map correction

Old: Closing Fee posts to Marketplace Other Charges (catch-all).
New: Closing Fee posts to Marketplace Closing Fees (specific account).

Effect: P&L granularity improves. Historical data is NOT retroactively reposted (that would require full re-recon of closed periods).

Migration playbook: applies to new transactions only. Existing closed-period balances stay in old accounts. New transactions go to new accounts. The FDE explains the discontinuity to the client's accountant.

### GST disposition change

Old: Weight Discrepancy Charges treated as ITC Claimable.
New: Weight Discrepancy Charges treated as ITC Conditional (claimable only if marketplace issues GST-compliant invoice; many do not).

Effect: claimed ITC will decrease for clients of marketplaces that issue debit notes (not GST invoices). This is a real reduction in the claimable ITC, but it's a correction of a previous over-claim — methodology team's correction prevents downstream tax-audit exposure.

Migration playbook: requires methodology lead's direct conversation with the client's CA. Migration cutover only after CA acknowledgment.

---

## Common mistakes

### Migrating without sign-off

Tempting to push the new Default to a client because "it's better." But methodology lifecycle has a sign-off bar for a reason — premature migration of a Pilot rule to a client is risky.

### Skipping the dry-run analysis

The dry-run on the client's last 3 months of data shows what would have been different. If this is skipped, the client is surprised by the post-migration behavior. Surprised clients trust the FDE less. Always run the dry-run.

### Migrating multiple rules simultaneously

A client migrates from v0.1 to v0.3 (skipping v0.2). The behavioral diff includes both v0.1→v0.2 and v0.2→v0.3 effects, which interact. Hard to attribute outcomes; hard to roll back if something goes wrong.

Better: migrate one rule at a time, with a stable period between migrations.

### Forgetting the methodology addendum

The addendum is the auditable record. Without it, six months later when the methodology team reviews per-client variances, this client's exception looks unauthorized. The addendum exists to document approved exceptions.

### Removing the deprecated rule's code path before all clients have migrated

Code path removal is the *last* step. If a client is still on the old rule when the code path is removed, their integration breaks.

The methodology lifecycle's `removal_at` date is the trigger; before that date, code path stays. After that date, clients still on the old rule are out of compliance and engineering escalates them.

---

## When you're done

For the methodology team's perspective on a migration cycle:

- New Default fixture is in the parent app
- Migration playbook is published
- Tests cover both old (with override) and new (without override) behaviors
- All FDE-fleet clients have migrated, or have formally retained the old rule via approved addendum
- After `removal_at` date, the deprecated code path is removed
- `CHANGELOG.md` records the cycle

For an FDE migrating one client:

- Client's `client_overrides.json` no longer has the old override
- `bench migrate` runs clean
- A test event in the client's deployment behaves per the new Default
- First week of post-migration activity is monitored
- Client is notified of the migration date and the behavioral diff

In your chat response, name: which rule, old vs new value, who approved, the migration window, and the FDE owning per-client transitions.
