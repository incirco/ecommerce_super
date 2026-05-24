# Playbook: Handling a Schema Drift Alert

**When to use this:** A schema drift alert has fired (Info or Warning severity, depending on Jaccard distance per `SPEC.md` Section 22.4). EasyEcom has produced a new payload shape we haven't seen before. The integration is still processing — schema drift doesn't immediately break things — but if we ignore it, silent mis-mapping will accumulate.

**Audience:** This is primarily an FDE-facing playbook for runtime operations, but Claude Code may be asked to assist or to scaffold the response.

**Pre-flight check:** Open the alert. Read the Translated Title and Suggested Actions. The alert should link directly to the EasyEcom Schema Snapshot record. If it doesn't, the Error Translation library coverage has a gap — note it and proceed.

---

## What schema drift means

Every API response and every webhook payload is hashed by *shape* (paths and types, not values) into a `EasyEcom Schema Snapshot` record. New shapes produce alerts, with the Jaccard distance against the closest known-good shape determining severity:

- Distance < 0.05 — minor variance, Info severity, often a one-off
- Distance 0.05-0.1 — Notable, Info severity, likely a payload variant for some condition
- Distance > 0.1 — Substantial, Warning severity, likely an API change

**Drift doesn't break the integration immediately.** The existing Field Mapping rules continue to process it; new fields are silently dropped (in Permissive mode) or raise (in Strict mode). The alert is your chance to catch the change *before* it produces silent mis-mapping.

---

## The 6-step response

### 1. Open the Schema Snapshot record

The alert links to it. The record carries:

- `snapshot_hash` — the shape's identifier
- `endpoint` — which EE endpoint produced this shape
- `direction` — Outbound Response, Inbound Webhook, or Inbound Pull
- `first_seen_at` and `observation_count`
- `paths_summary` — the JSON list of (path, type) pairs that constitute this shape
- `distance_to_known_good` — the Jaccard distance vs the closest blessed shape
- `sample_payload_link` — link to the first redacted payload sample

### 2. Open the diff view

In the desk, on the Schema Snapshot record, click "View Diff vs Closest Known Good." The desk shows a side-by-side:

- Paths in this new shape vs paths in the closest known-good shape
- Highlighted: added paths (green), removed paths (red), type changes (yellow)

The output of this comparison answers the key question: *what specifically is different about this shape?*

### 3. Categorise the drift

Three categories, three different responses:

| Category | Symptom | Response |
| --- | --- | --- |
| **Benign extension** | EE added a new optional field; nothing important changed | Mark Known Good, no further action |
| **Material change** | A field we map was renamed, a field's type changed, a required field was dropped | Update the Field Mapping ruleset |
| **Critical change** | A path we depend on for recon (tax fields, settlement IDs, order amounts) changed | Stop, escalate, FDE + methodology team review |

The mental model: if the diff would change the output of any active Field Mapping ruleset, it's at least Material. If it changes the output of a recon-critical field, it's Critical.

### 4. Inspect the actual payload sample

Click "View Sample Payload" on the snapshot. Compare against:

- A known-good sample from the closest matching snapshot
- The Field Mapping ruleset for this endpoint (which paths does the ruleset reference?)

Ask: are any of the changed paths referenced by an active Field Mapping rule?

If yes — the rule needs updating. Go to step 5.

If no — the change is genuinely benign at the mapping layer. But check downstream: does the recon engine read raw payloads anywhere (it shouldn't, but check)? Does any flow assume a path that's no longer present?

### 5. Update the Field Mapping ruleset (if Material)

Follow the playbook `adding_a_field_mapping_rule.md` to add or modify rules. Specifically:

- If a path was renamed: add a new rule with the new path; mark the old rule deprecated (don't delete — drift may be reversible)
- If a type changed: add or modify the transformer
- If a new field appeared and we want to capture it: add an explicit rule (don't rely on identity matching)
- If a field disappeared: if our rule has `required_field: 1`, the rule will start failing. Decide whether to relax to `required_field: 0` with a sensible default, or whether the disappearance is an EE bug to escalate.

After the ruleset is updated, the new save creates a Field Mapping Version (per `SPEC.md` Section 20.12) and the Configuration Audit captures who changed what.

### 6. Mark the Schema Snapshot Known Good

Once you've handled the drift (either by accepting the new shape as benign, or by updating the ruleset to handle it), set `is_known_good=1` on the Schema Snapshot record. Add `fde_notes` explaining what the drift was and how it was resolved.

Future occurrences of this shape will not alert.

---

## Tools available

### Mapping coverage report

`SPEC.md` Section 22.6. Shows what % of fields in real payloads are mapped explicitly vs identity-matched vs dropped. After a drift, run the coverage report — sustained increase in dropped % indicates the ruleset is getting stale.

### Drift inspection action: "Affects Field Mapping"

In the Schema Snapshot detail page, the "Affects Field Mapping" action surfaces which active Field Mapping rulesets reference paths that changed in this drift. This is the fastest way to scope the response.

### Inspector for a specific record

If the drift was detected on a specific webhook event or API call, the Inspector (`SPEC.md` Section 18.9) shows the full payload, the resolved Field Mapping output, and any downstream documents. Useful for "did this drift cause anything bad to happen?"

---

## Common patterns and their responses

### "EE added a new field with information we want"

Example: EE adds `manifest_courier_partner` to the manifest webhook. This is information we currently don't capture but might want to.

Response:
1. Add a Field Mapping rule mapping the new EE path to a new ERPNext path (probably a custom field on Sales Invoice with `ecs_` prefix)
2. Add the custom field to `fixtures/custom_field.json`
3. Update tests
4. Mark Known Good

### "EE renamed a field"

Example: EE renames `taxAmount` to `taxValue` in order responses.

Response:
1. Update the Field Mapping rule's `easyecom_path` to the new name
2. The old rule should not be deleted — it's possible EE rolls back, and removing the rule loses the documentation. Mark the rule's `notes` field to record the rename.
3. Run an integration test against the new shape to confirm
4. Mark Known Good

### "EE changed a type"

Example: EE changes `weight` from String "0.5" to Float 0.5.

Response:
1. Add a transformer to the rule (`transform_pull: str_to_float`) — but check what the new shape actually is via the Sample Payload
2. Test that round-trip behavior still works
3. If the type change is material to a downstream computation, also test the downstream
4. Mark Known Good

### "EE removed a field we depended on"

Example: EE stops including `gst_invoice_number` in order responses.

Response — this is Critical:
1. **Do not silently relax the rule.** The methodology may depend on this field for downstream output. Check.
2. Open a methodology team review request.
3. If the field is truly removed and unrecoverable, the methodology team decides whether the affected functionality is degraded, falls back to a derived value, or raises a Discrepancy per missing record.
4. Update the rule per the methodology team's direction.
5. Mark Known Good only after methodology sign-off.

### "Drift on a webhook signature field"

Example: EE changes the HMAC signing scheme.

Response — this is also Critical, but for security reasons:
1. Webhook receipts may now fail signature verification, returning 401
2. Coordinate with EE support to understand the new scheme
3. Update `api/webhook.py` signature verification logic
4. This is a parent-app code change, not a fixture change — surface to engineering team

---

## When NOT to mark Known Good

Some drifts should NOT be auto-blessed. Surface them to the user instead:

- Drifts on auth-related endpoints (login response, token refresh response)
- Drifts on webhook payloads where signature is recomputed
- Drifts that reduce information (a field disappearing rather than appearing)
- Drifts that change types in ways that break round-trip behavior
- Drifts on endpoints that recon engine reads directly (rare; recon should always read via Field Mapping, but verify)

In these cases, the playbook's correct response is: **escalate to the user with a clear summary of what changed, what the impact assessment is, and what options exist.**

---

## Common mistakes

### Auto-blessing drift to make the alert go away

Tempting when you have many alerts. Wrong. Each drift represents a real change EE made; understanding it is the value-add. Auto-blessing without inspection is how silent mis-mapping accumulates.

### Updating the rule without updating the test

Field Mapping changes need test changes. A rule update with no corresponding test update will break (or worse, succeed) silently when EE drifts again.

### Ignoring the "Affects Field Mapping" indicator

If the indicator says "no Field Mapping rules affected," it's tempting to skip the inspection. Don't. The reason "no rules affected" is informative is also a reason to verify — perhaps a rule *should* be affected but isn't because we never mapped that path.

### Forgetting that drift can be backwards-compatible

A new field added to a payload doesn't break anything. Identity matching in Permissive mode just drops it. Marking Known Good is fine. The opposite — a field removed — is the dangerous case.

---

## When you're done

- Schema Snapshot has `is_known_good=1` and meaningful `fde_notes`
- If a Field Mapping rule was updated, a new Field Mapping Version exists with the change reason
- If a custom field was added, the fixture and migration are clean
- Tests cover the new shape
- The Mapping Coverage Report shows expected impact (or no impact, if benign)

In your chat response, summarise: which shape drifted, the diff (added/removed/changed paths), the response category (benign/material/critical), the actions taken.
