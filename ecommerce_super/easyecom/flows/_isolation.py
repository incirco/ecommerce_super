"""Per-record savepoint isolation for batch flows.

SPEC §7.1: "Per-record isolation is mandatory: a handler processing a batch
wraps each record in its own savepoint, so one record's exception can never
abort its siblings or the surrounding transaction. A record that raises is
caught, its Sync Record is marked Failed, and processing continues to the
next record."

This module is the canonical implementation of that contract. Every batch
flow in the integration (Location discovery is the first; Channel, Item,
Customer, Supplier, Order, GRN, Return all follow) calls `for_each_record`
to drive its inner loop. Building this once, here, guarantees the §7.3
binary per-record outcome is uniformly enforced — a record either succeeds
cleanly or is recorded Failed, never the third option of "siblings rolled
back too" or "partial state left behind."

Why savepoints (and not nested transactions):

  Frappe runs each request inside one database transaction. A handler that
  raises causes the whole request transaction to roll back unless the
  exception is caught at a savepoint boundary. `frappe.db.savepoint(name)`
  + `frappe.db.rollback(save_point=name)` lets us roll back ONLY the work
  done since the savepoint, leaving prior records' writes intact.

  This is materially different from a per-record try/except that doesn't
  use savepoints: without the savepoint, a database write made before the
  raise lands in the transaction and travels with the next record's
  writes — corrupting the all-or-nothing per-record contract.

Two-axis design — the helper is general:

  - `handler(record)`: caller-supplied function that does the per-record
    work. May write to any DocType; may raise any exception.
  - `on_failure(record, exc)`: caller-supplied callback fired AFTER the
    savepoint rollback, so the caller can record the failure (typically:
    write/update a Sync Record to status=Failed, or surface to a queue
    job's failed_count) WITHOUT being part of the rolled-back work.

The callback runs OUTSIDE the savepoint so the failure record itself
survives the rollback. Without this split, marking the record Failed
would itself be rolled back by the rollback, and the failure would
disappear silently — the exact §2.7 violation the contract forbids.

Usage:

    from ecommerce_super.easyecom.flows._isolation import for_each_record

    outcome = for_each_record(
        records,
        handler=lambda r: upsert_location(r),
        on_failure=lambda r, e: record_failed(r, e),
        flow_name="location_discovery",
    )
    # outcome.succeeded: list of records that committed cleanly
    # outcome.failed:    list of (record, exception) pairs that rolled back
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import frappe

# MariaDB savepoint names accept letters, digits, and underscores. Anything
# else (dashes, dots, spaces) causes a SQL syntax error at SAVEPOINT time.
# We sanitise flow_name once per call to avoid surprising the caller.
_SAVEPOINT_SAFE_RE = re.compile(r"[^A-Za-z0-9_]+")


@dataclass
class BatchOutcome:
    """Result of running for_each_record on a batch.

    A Partial Queue Job (§7.3) is the case where both lists are non-empty.
    All-success or all-fail collapses to a single non-Partial state on the
    parent Queue Job per the §7.3 interlock.
    """

    succeeded: list[Any] = field(default_factory=list)
    failed: list[tuple[Any, BaseException]] = field(default_factory=list)

    @property
    def succeeded_count(self) -> int:
        return len(self.succeeded)

    @property
    def failed_count(self) -> int:
        return len(self.failed)

    @property
    def total(self) -> int:
        return self.succeeded_count + self.failed_count


def for_each_record(
    records: Iterable[Any],
    *,
    handler: Callable[[Any], None],
    on_failure: Callable[[Any, BaseException], None] | None = None,
    flow_name: str = "batch",
) -> BatchOutcome:
    """Run `handler(record)` for each record inside its own savepoint.

    A handler exception rolls back ONLY that record's writes and records
    the failure via `on_failure(record, exc)`. Siblings are unaffected:
    records before the failure remain committed, records after are
    processed normally.

    Args:
        records: any iterable of caller-defined record objects (dict,
            dataclass, doc, primitive — opaque to this helper).
        handler: invoked once per record. May write any docs. May raise
            any exception (the helper catches BaseException's subclasses
            except those Python itself reserves — SystemExit /
            KeyboardInterrupt propagate).
        on_failure: invoked AFTER savepoint rollback for each failed
            record. Anything it writes (typically a Failed Sync Record)
            is OUTSIDE the rollback and therefore survives. Defaults to
            None, meaning failures are silently dropped — pass an
            explicit callback in production paths.
        flow_name: short identifier mixed into savepoint names for
            debuggability when Frappe prints them. No semantic meaning.

    Returns:
        BatchOutcome with succeeded / failed lists.

    Why we catch Exception (not BaseException): SystemExit and
    KeyboardInterrupt should kill the worker, not be swallowed as
    per-record failures.
    """
    outcome = BatchOutcome()
    safe_flow = _SAVEPOINT_SAFE_RE.sub("_", flow_name) or "batch"

    for idx, record in enumerate(records):
        # Savepoint names must be unique within a transaction. The flow
        # name + index keeps names readable in the DB error log without
        # constraining call patterns. MariaDB savepoint identifiers are
        # alphanumeric + underscore only — `safe_flow` sanitises the caller's
        # flow_name once so dashes/spaces/dots don't blow up SAVEPOINT.
        savepoint = f"sp_{safe_flow}_{idx}"
        frappe.db.savepoint(savepoint)
        try:
            handler(record)
        except Exception as exc:
            # Roll back ONLY this record's writes — siblings keep theirs.
            frappe.db.rollback(save_point=savepoint)
            outcome.failed.append((record, exc))
            # Record the failure OUTSIDE the rollback so it survives.
            if on_failure is not None:
                try:
                    on_failure(record, exc)
                except Exception as on_fail_exc:
                    # An on_failure callback that itself raises is a bug
                    # in the caller, not a per-record failure. We log and
                    # continue — never let one record's failure-reporting
                    # crash the whole batch.
                    frappe.log_error(
                        title=f"for_each_record: on_failure raised for {flow_name}",
                        message=(
                            f"Record index {idx}: original exception "
                            f"{type(exc).__name__}: {exc}\n"
                            f"on_failure raised "
                            f"{type(on_fail_exc).__name__}: {on_fail_exc}"
                        ),
                    )
        else:
            outcome.succeeded.append(record)

    return outcome
