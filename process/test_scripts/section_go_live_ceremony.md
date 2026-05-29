# Test Script — Go Live / Pause Auto-Push Ceremony

*Goes in `process/test_scripts/section_go_live_ceremony.md`. Same format as the §8 master test scripts. Covers the account-wide auto-push controls shipped in round 2 (commit `3c33c58`).*

> **Status:** Round-2 hardening. The three `auto_push_*_on_save` toggles ship OFF for safety; these two actions are the go-live/pause levers. Role-gated, confirm-required, audit-logged.

**Prerequisites:** At least one master (Item / Customer / Supplier) configured and mapped on the account. Test EasyEcom credentials configured. Log in as each of: an FDE (or System Manager), and an Operator, to test the role gate.

**A note on what's live vs mocked:** these actions flip checkboxes and write audit Comments on the Account doc — no EE writes happen at flip time. Auto-push *itself* (what the toggles enable) is the per-master push, governed by the project's no-live-write rule. This script tests the ceremony, not the downstream push.

---

## Section 1 — Go Live

### 1.1 Go Live enables all three toggles
**Do:** As an FDE, on EasyEcom Account, click **Go Live** (enable auto-push). In the dialog, leave all three masters selected. Confirm.
**Confirm:** The three `auto_push_items_on_save` / `auto_push_customers_on_save` / `auto_push_suppliers_on_save` checkboxes all flip ON. An audit Comment appears on the Account doc naming who, when, and which masters were enabled.
**Good:** All three ON; audit Comment present.
**Failure looks like:** Toggles unchanged (action didn't apply — report), or no audit Comment (audit wiring missing — report).

### 1.2 Go Live with a master deselected
**Do:** First Pause All (Section 2.1) to reset. Then click **Go Live**, deselect e.g. Suppliers, confirm.
**Confirm:** Items + Customers flip ON; Suppliers stays OFF. Audit Comment reflects the partial enable.
**Good:** Only the selected masters enabled; audit accurate.

### 1.3 Confirm is required
**Do:** Click **Go Live** and dismiss/cancel the confirm dialog.
**Confirm:** Nothing changes — no toggle flips, no audit Comment.
**Good:** Cancelling is a true no-op.

---

## Section 2 — Pause

### 2.1 Pause All disables every toggle
**Do:** As an FDE, click **Pause All Auto-Push**. Supply a reason (e.g. "EE maintenance window"). Confirm.
**Confirm:** All three toggles flip OFF. Audit Comment records who, when, and the reason.
**Good:** All OFF; reason captured in audit.
**Failure looks like:** A toggle stays ON (incomplete pause — report), or reason not recorded.

### 2.2 Pause is reversible via Go Live
**Do:** After a Pause All, run Go Live again.
**Confirm:** Toggles return to ON; both the pause and the re-enable show in the audit history.
**Good:** Full round-trip; audit shows both events.

---

## Section 3 — Role gate

### 3.1 Operator cannot Go Live or Pause
**Do:** Log in as an Operator. Attempt **Go Live** and **Pause All**.
**Confirm:** Both actions are refused (button hidden/disabled, or a permission error on invoke). No toggle changes.
**Good:** Operator is blocked from both; FDE/SM are not.
**Failure looks like:** Operator can flip the account-wide auto-push state (permission leak — report immediately).

---

## What "passing" means

The ceremony passes when: Go Live enables the selected masters in one confirmed, audited action; deselecting a master in the dialog is honoured; Pause All disables everything with a recorded reason; the pause/enable round-trip is clean and fully audited; and the Operator role is blocked from both while FDE/SM are not. The most important check: **3.1 (Operator cannot change account-wide auto-push state)** — this is the safety boundary that keeps a non-FDE from accidentally going live on a client account.
