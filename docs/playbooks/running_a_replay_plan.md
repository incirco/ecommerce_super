# Playbook: Running a Replay Plan

**When to use this:** A flow has failed for many records and you need to recover. Examples: missed manifest webhooks during a 4-hour EE outage; failed Item pushes after a credential rotation; a Field Mapping bug produced bad payloads for a day; settlement events processed before a methodology rule fix.

**Audience:** Primarily FDE-facing for runtime ops; Claude Code may assist in scaffolding the plan or debugging the dry-run.

**Pre-flight check:** Read `SPEC.md` Section 21 (Replay and Recovery Tooling). The Replay Plan lifecycle (Draft → Dry Run → Reviewed → Committed → Completed) is non-negotiable.

---

## When NOT to use a Replay Plan

Replay Plans are for *batch* recovery. For one or two records, just use the per-record action menu (Retry Now, Force Resync) on the Sync Record. The overhead of a Replay Plan isn't justified at small scale.

Use a Replay Plan when:
- The fix needs to apply to >5 records uniformly
- The records share a common filter (failed since X, error contains Y, affected SKU group)
- You need a dry-run preview before committing
- You want a permanent record of what was done

Don't use a Replay Plan when:
- A single record needs a one-off retry — use the action menu
- The fix per record is different — multiple Replay Plans, not one
- The records are spread across Companies — one Replay Plan per Company

---

## The 7-step lifecycle

### 1. Diagnose first

Before creating any plan, understand what failed and why. Open:

- The Morning Brief (`SPEC.md` Section 26) — does it reference this incident?
- The relevant Sync Records' `last_error_translation_key` — does the Error Translation library tell you what's wrong?
- The Event Timeline (`SPEC.md` Section 18.8) for affected records — is there a pattern?
- Schema Drift recent alerts — did EE change something?

Don't replay until you've identified the root cause. **Replaying without fixing the root cause produces another batch of identical failures.**

### 2. Choose a strategy

Five strategies, each suited to a specific situation (`SPEC.md` Section 21.2):

| Strategy | Use when | Don't use when |
| --- | --- | --- |
| **Retry-As-Is** | Transient failure (rate limit, brief EE outage); records and config are correct | The original error was a permanent validation failure — re-running won't change the result |
| **Retry-With-Override** | Source records have a fixable defect; faster to override at retry time than to fix the source data | The defect is in the Field Mapping ruleset itself; fix the rule first |
| **Reprocess** | Webhook ingestion logic was bug-fixed since the original webhook arrived | The webhook itself was malformed at the source |
| **Force-Resync** | Suspected silent drift — both sides claim current but differ | You don't have a baseline of which side is correct |
| **Mark-Manually-Resolved** | The issue was fixed out-of-band (manually edited EE record); the integration just needs to stop trying | Records still genuinely need to sync |

Most replays are Retry-As-Is or Retry-With-Override. The others are specialised; use deliberately.

### 3. Construct the filter

The filter selects affected records. It uses Frappe's standard filter syntax plus replay-specific extensions (`SPEC.md` Section 21.4):

```json
{
  "target_doctype": "EasyEcom Webhook Event",
  "filter_expression": {
    "company": "Acme Corp",
    "event_type": "manifest.created",
    "received_at": [">", "2026-04-30 14:00:00"],
    "received_at": ["<", "2026-04-30 18:00:00"],
    "processing_status": "Failed"
  }
}
```

Use the **Preview Count** button on the Replay Plan form. It runs the filter without committing and tells you how many records will be affected. **Always preview before configuring strategy.** A filter that selects 10,000 records when you expected 50 is a sign the filter is wrong.

If the count is surprising (much higher or lower than expected), refine the filter:
- Add explicit time bounds
- Add `error_contains` or `translated_error_key` to scope to specific failure types
- Use compound filters (AND/OR) rather than relying on a single field

### 4. Run the dry run

Click "Run Dry Run." This:

- Performs all read operations (fetches source data, evaluates Field Mapping, computes payloads)
- Does NOT perform any write operations
- Captures predicted outcomes per record: would-succeed / would-fail / would-no-op (with reason)
- Captures the payload that would be sent

Dry run results are persisted in the `dry_run_results` field on the Replay Plan. Open the Dry Run Review surface (`SPEC.md` Section 21.3.1):

- Summary at top: predicted success count, predicted failure count, predicted no-op count
- Per-record table with predicted outcome, predicted error if applicable
- "View Sample Payload" inline — verify what would be sent

**Common dry-run outcomes and what they mean:**

| Dry-run output | Meaning | Next action |
| --- | --- | --- |
| 100% predicted success | Strategy and overrides are correct | Sign off and commit |
| Mixed (some success, some predicted failure) | Some records have an additional issue not covered by your fix | Investigate the predicted failures; refine the filter to exclude or refine the strategy |
| 100% predicted failure with same error | Your fix doesn't address the root cause | Stop. Re-diagnose. Re-think the strategy |
| 100% no-op | Records are already in the desired state | Cancel the plan; nothing to replay |

### 5. Review and sign off

For Retry-With-Override, click "Compare Against Original" on a sample record to see the side-by-side diff (original payload vs overridden payload). Confirm the override is what you intended.

If the dry run is acceptable, click "Sign Off Dry Run." This:

- Records the FDE who signed off and the timestamp
- Unlocks the Commit phase

If the dry run is unacceptable, cancel the plan or revise the filter/strategy and re-run dry.

**Don't sign off if you're not confident.** A signed-off plan that goes wrong is harder to recover from than a paused one.

### 6. Commit

Click "Commit." Required to populate `commit_reason` (a free-text field explaining why this replay was needed — for audit). Commit is gated on:

- Dry Run completed successfully
- Dry Run sign-off recorded
- `commit_reason` populated

For plans affecting >100 records or financial impact >₹100k, Commit requires the Replay Approver role (per `SPEC.md` Section 21.7). If you don't have the role, the Commit button is disabled — surface the plan to a senior FDE or methodology lead for sign-off.

Commit creates a parent Queue Job that spawns child Queue Jobs per affected record, throttled per the plan's `throttle` setting. The plan's state advances through Commit Running → Completed.

You can pause or cancel mid-commit. The system stops issuing new operations, but does not roll back already-committed ones. **A partially-committed replay is hard to reason about.** Avoid pause/cancel unless the situation is bad enough that continuing makes it worse.

### 7. Verify the outcome

After commit completes:

- Read the `commit_results` field — per-record actual outcomes
- Spot-check a sample of affected records: open the Sync Record, confirm `sync_status=Success` and the affected document is in the expected state
- Verify the related Discrepancies (if any) are now closed
- Check the Morning Brief — the recovered exposure should now reflect

If outcomes don't match dry-run predictions, investigate. Common reasons:
- The world changed between dry-run and commit (new webhooks arrived, EE state shifted)
- A subset of records had a defect not caught in dry-run
- An EE-side rate limit kicked in mid-commit (look at API Calls)

---

## Common patterns

### Replay missed manifest webhooks for a time window

```
target_doctype: EasyEcom Webhook Event
filter:
  event_type: "manifest.created"
  received_at: [between 2026-04-30 14:00, 2026-04-30 18:00]
  processing_status: ["in", ["Failed", "Pending"]]
strategy: Reprocess
```

Most common replay pattern. Webhooks were received but processing failed (or didn't run); reprocess them with current ingestion logic.

### Retry all failed Item pushes whose error mentions HSN

```
target_doctype: EasyEcom Sync Record
filter:
  entity_type: "Item"
  direction: "Push to EE"
  sync_status: "Failed"
  error_contains: "HSN"
strategy: Retry-As-Is
```

The implicit prerequisite: the FDE has added the missing HSN codes to the Tax Master before running this replay. Without the prerequisite fix, the dry-run will predict failure.

### Force-resync a set of drifting SKUs

```
target_doctype: EasyEcom Sync Record
filter:
  entity_type: "Item"
  ecs_drift_detected: 1
strategy: Force-Resync
```

Useful when you know the drift exists (e.g., a custom report shows ERPNext stock and EE stock disagree for 47 SKUs). Force-Resync clears hashes and re-syncs.

### Mark a batch as resolved out-of-band

```
target_doctype: EasyEcom Sync Record
filter:
  entity_type: "Item"
  sync_status: "Failed"
  erpnext_name: ["in", [list of 47 item codes]]
strategy: Mark-Manually-Resolved
override_values: {resolution_note: "These items exist on EE but we no longer sell them; closing without sync"}
```

Always requires Replay Approver. Always requires resolution_note.

---

## Common mistakes

### Skipping dry run

Tempting when "the fix is obvious." Doesn't matter — dry run is the safety check. Production data is expensive to corrupt.

### Replaying without fixing the root cause

Replay says "do it again." If the cause is unfixed, "again" produces the same failure. Diagnose first.

### Filter too broad

A filter that selects records you didn't intend. The Preview Count button catches this. Use it.

### Filter too narrow

A filter that misses some affected records. After commit, you discover 23 more records that should have been included. Now you need a second Replay Plan. Avoid by using broader filters in dry-run, then narrowing if needed.

### Committing without reading commit_results

Commit completes; you walk away. Then the next morning's Brief shows new failures from the replay. Always check `commit_results` and verify per-record outcome.

### Pausing mid-commit unnecessarily

A pause leaves the system in a half-state. Records before the pause point are committed; after are not. Reasoning about subsequent retries gets complex. Pause only if continuing causes worse harm.

---

## When you're done

- Replay Plan in state Completed
- `commit_reason` and audit trail populated
- Affected Sync Records show expected outcome
- Related Discrepancies closed (if applicable)
- Recovered financial impact reflected in the Morning Brief / SLA Compliance dashboards

In your chat response (or in the user's incident review), summarise:
- What broke (root cause)
- What you replayed (filter + strategy + count)
- Dry-run vs commit outcome
- Recovered financial impact
- Any followup items (e.g., methodology rule update, Field Mapping fix, monitoring gap)

The Replay Plan record itself is now an audit artefact — don't delete it. Plans persist indefinitely.
